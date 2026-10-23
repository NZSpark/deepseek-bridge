# 历史经验与踩坑（只追加）

> 摘要：每条含「症状 / 根因 / 判据 / 防复发」。判据必须能观察，不能是「应该没问题」。
> 排错时先查本文件，再看 doc/update.md。

---

## L01 · 背景启动卡死父 shell

- 症状：start_all_bridges.sh start 永不返回，像死循环。
- 根因：( cd … && nohup cmd >>log 2>&1 & echo $! >pid ) —— & 只绑定了 nohup 段，
 子 shell 仍在前台、stdin 未断开，父 shell 一直等。
- 判据：脚本执行超过 60s 无输出。
- 防复发：setsid cmd </dev/null >>log 2>&1 &，三件套重定向必须齐。

## L02 · $SECONDS 进子 shell 会重置

- 症状：while (( SECONDS < deadline )) 在子 shell 里不收敛。
- 根因：SECONDS 是 shell 变量，子 shell 继承值但独立递增；配合前述卡死叠加成死循环。
- 判据：循环体里没有副作用时 CPU 占满且不退出。
- 防复发：等待循环用递减计数器 n=$((S*3)); while (( n-- > 0 ))。

## L03 · 闭合的 DSML 块也可能是坏的

- 症状：<｜｜DSML｜｜ calls>…</｜｜DSML｜｜ calls> 完整闭合，但解析层救不出调用。
- 根因：块内 JSON 字符串里夹了裸换行（python3 -c⏎'...'），JSON 不允许裸换行。
- 判据：parse_tool_calls(text) 返回 0 且 DSML_CLAIM.search(text) 为真。
- 防复发：判据只看解析结果，不看块是否闭合；测试里要有「闭合但内容坏」的用例。

## L04 · 旧 [x] 比没有更坏

- 症状：doc/toolcall_retry_tasks.md 几乎全是 [ ]，但代码早已落地。
- 根因：文档按「历史计划」标注而非「当前代码能力」标注。
- 判据：文档说缺某能力，但 grep 能在源码里找到实现。
- 防复发：任务清单只按代码实际状态标；每条 [x] 要能指到文件:行号。

## L05 · 不确认共享源就报「已提交」= 假成功

- 症状：git ls-files skills/ 为空——整个 skill 目录从未进版本库。
- 根因：skills/ 是本地副本（.gitignore 未列但从未 git add），改完不会进 git。
- 判据：git status --short <path> 有改动但 git ls-files <path> 为空。
- 防复发：报「已提交」前必须跑 git ls-files 确认；skill 改动只说「本地已改」。

## L06 · pytest 收集失败 ≠ 测试失败

- 症状：ModuleNotFoundError: No module named 'scripts.recon'，报 2 个 collection error。
- 根因：测试用 from scripts.recon import …，但 scripts/ 无 __init__.py，且 rootdir 不在 skill 目录。
- 判据：错误信息里是 ERROR collecting 而非 FAILED。
- 防复发：scripts/__init__.py + 在 skill 目录内跑 pytest。

## L07 · 工具调用串行时旧文本可能已失效

- 症状：edit 报「Could not find the exact text」，但内容看起来一致。
- 根因：前一次 write/edit 已改过该区域，oldText 是改前的。
- 判据：报错指向的行号附近内容与 oldText 不完全相同（空白/换行差异也算）。
- 防复发：多处编辑用一次 edit call 的多个 entries（都基于原始文件匹配），或先 read 确认。

## L08 · 改函数返回形状必须全库扫调用点，测试用假对象会掩盖

- 症状：`complete_with_toolcall_retry` 从返回 `(reply, blocks)` 改为只返回 `reply`，
  测试全绿、已提交，线上每个流式请求却崩 `ValueError: too many values to unpack`。
- 根因（两层）：
  1. 只 grep 了 `tests/` 找调用点，漏掉 `deepseek_web/streaming.py` 这个同包内部调用点；
  2. 相关测试用假 driver 直接返回值，`_stream_chat_completion` 的**真实**解包路径
     从未被执行，形状不一致时测试仍是绿的。
- 伴生 bug：改正常分支的 queue 载荷为 3 元组，却漏改 5 个 `except` 分支的 4 元组，
  消费端解包失败 —— 只在异常路径触发，跑正常用例永远看不见。
- 判据：`grep -rn 'func_name(' deepseek_web/ tests/` 有任一未同步的调用点；
  或测试里出现 `driver` 假对象直接返回新旧两种形状都「能过」的情况。
- 防复发：
  1. 改签名先全库扫：`grep -rn 'func_name(' deepseek_web/ tests/ --include=*.py`，
     不要只搜测试目录；
  2. 返回形状类改动必须有一条**不 mock 该函数**的测试（让真实解包跑一遍）；
  3. 同一数据结构有多个生产点（正常分支 + 各 except 分支）时，逐个核对元组长度；
  4. 报「测试全绿」前必须真的跑一次全量，而不是只跑新增用例。
