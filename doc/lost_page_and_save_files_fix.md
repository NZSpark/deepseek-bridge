# 参考：会话页面失效自愈 + 落盘签名（跨项目移植版）

> 来源：DeepseekBridge 2026-10-09 的真机故障修复（`doc/update.md` §七 P0-J / P0-K / P0-L）。
> 姊妹项目（ChatGPTBridge / GeminiBridge 等）架构同源，同类缺陷可以照本文逐条核对与移植。
> 本文只写**与具体站点无关**的模式；平台差异（选择器、模式选择、开新对话）不在此列。

---

## 0. 症状清单（命中任意一条就来核对本文）

- 某个会话桶的请求**几乎立刻**失败（0.x 秒，而不是等满选择器超时），错误却是
  「无法找到对话输入框，请检查网页是否打开或处于登录状态」。
- 同一时刻**别的**会话桶请求正常 → 说明不是站点改版、不是未登录。
- 该桶之后**每次都失败**，重启进程才恢复（说明页面池里留着一具尸体）。
- 「回复已经完整生成、客户端却收到 500」，日志里是
  `TypeError: save_extracted_files() takes 3 positional arguments but 4 were given`。
- 偶发/随机 502：重试时或别的桶建页时正常，唯独并发的那些失败。

---

## 1. 三个根因（按危害排序）

### P0-J　页面失效后仍被当成「可用页面」

**机制**：页面池 `_pages` 只记录「这条页面是我们创建的」，**不保证标签还活着**。
用户关掉标签 / 渲染进程崩溃 / 浏览器回收后台标签后，页面对象仍在池里，此后
`page.wait_for_selector` 会**立刻**抛错（`Target page, context or browser has been closed`
/ `Target crashed`）。而定位输入框的函数通常写成「逐个选择器 try/except，全不中就返回 None」，
于是这个异常被归到「选择器都没命中」；`_ensure_page` 又只判断
`bucket in self._pages` → **永不重建**，该桶从此刻起永久 502。

**判定（关键，决定要不要按本文改）**：

| 观察 | 结论 |
| --- | --- |
| 失败耗时 ≈ 0.x 秒 | 页面对象已死（异常在 `wait_for_selector` 里立刻抛） |
| 失败耗时 ≈ 轮数 × 选择器数 × 超时（如 3×4×2s ≈ 24s） | 页面活着但真的没有输入框（改版 / 非对话视图 / 弹层 / 未登录） |
| 日志里搜 `Target`、`closed`、`crash` | 就是本文这条；多数实现会把原始异常写进「尝试过的选择器」列表里 |

**修复模式**：

1. **加判活**：`page is not None and not page.is_closed()`（没有 `is_closed` 的实现按存活处理，
   否则测试替身会被误判）。崩溃的渲染进程常常仍报 `is_closed() == False`，所以判活只是**快路径**，
   不能当作唯一判据。
2. **区分异常**：定位输入框时，`TimeoutError` 才算「没命中」；其它异常（页面已关闭 / 崩溃）
   要抛**专用异常**（如 `DeepSeekPageLostError(RuntimeError)`），与「改版 / 未登录」彻底分开。
   这样上层才知道该「重建页面」而不是「提示用户去登录」。
3. **统一重建入口**：`_ensure_page(bucket)` 发现已登记的页面死了 → 关掉它（从 `_pages` 移除）
   再重建，**必须回到状态里保存的会话 URL**（`state.url`），这样网页会话的上下文不丢；
   重建后按真实落点重算 `has_history`（决定本轮是否需要「播种」）。
4. **默认桶（不参与分桶的那条页面）也要能重建**：它死了没有任何退路，整桥会永久 502。
5. **重试阶梯接纳它**：`send_chat` 捕获专用异常 → 重建页面 → **跳过**「恢复同一会话 / 换新会话」
   分支，直接重发（否则会把刚恢复的会话又轮转掉，白白丢上下文）。
6. **生成过程中页面失效**：轮询里的 `TargetClosedError` 不是 `RuntimeError`，会冒到路由层变成
   **裸 500**（连错误文案都没有）。在轮询每轮开头判活，并把它归到同一个专用异常。
7. **别只给一句「请检查是否登录」**：定位失败时附上现场信息——URL、`document.readyState`、
   各候选选择器命中数、是否有 `[role=dialog]` 弹层、是否像登录墙（**只回布尔，不回显正文**）。

### P0-K　`save_extracted_files` 少了 `self` / `@staticmethod`

**机制**：定义写成 `def save_extracted_files(raw_text, code_blocks, output_dir)`（无 `self`），
调用方按 `driver.save_extracted_files(raw, blocks, dir)`（实例属性访问）或
`asyncio.to_thread(driver.save_extracted_files, raw, blocks, dir)`（绑定方法引用）调用 →
实例占掉第一个位置参数 → `TypeError: takes 3 positional arguments but 4 were given`。
**回复已经生成完、页面也正常**，客户端却在最后一步拿到 500。

**为什么既有测试抓不到**：测试都按「类上未绑定调用」写（`Driver.save_extracted_files(a, b, c)`），
那条路径恰好不传实例，所以永远绿。**修完必须补一条「按实例调用」的用例**。

**修复**：改成 `@staticmethod`（三个位置参数的签名不变，所有调用点无需改动）。
若该项目的落盘是**可开关**的（默认关），它平时是潜伏状态——一旦打开就每个请求都炸。

### P0-L　请求进行中的桶，页面会被别的桶顺手回收

**机制**：发送流程在**拿桶锁之前**就已经确定页面（`_ensure_page`），而空闲回收 / LRU 淘汰
只跳过「锁被持有 / 在活跃集合里」的桶 → 这个窗口里页面可能被别的桶关掉，
该请求随即报「找不到输入框」（表现成随机 502）。

**修复**：「有请求在飞」用**可重入计数**表达，而不是看锁：

```python
def _mark_bucket_active(self, bucket):          # sender 在 send_chat 一开始就调用
    self._active_counts[bucket] = self._active_counts.get(bucket, 0) + 1
    self._active_buckets.add(bucket)            # 保留：/healthz 的 busy 仍可用它

def _unmark_bucket_active(self, bucket):        # send_chat 的 finally 里调用
    left = self._active_counts.get(bucket, 1) - 1
    if left > 0: self._active_counts[bucket] = left
    else:
        self._active_counts.pop(bucket, None)
        self._active_buckets.discard(bucket)

def _bucket_busy(self, bucket):
    return bucket in self._active_counts or self._lock_for(bucket).locked()
```

计数而不是集合是必须的：锁与 send_chat 都会打标记，**内层先退出不能提前撤掉外层的保护**。

---

## 2. 自查命令（在新项目上先跑这几条）

```bash
# 1) 落盘签名：有 self 或 @staticmethod 吗？调用点的实参个数对得上吗（含 to_thread / partial）？
grep -rn "def save_extracted_files" -B2 --include=*.py . | grep -v .venv
grep -rn "save_extracted_files" --include=*.py . | grep -v .venv

# 2) 有没有「页面存活」这个概念？完全没有 = 本文 P0-J 必然存在
grep -rn "is_closed\|page_lost\|PageLost" --include=*.py . | grep -v .venv

# 3) 页面池是不是只看「登记过」？
grep -rn "in self._pages" --include=*.py . | grep -v .venv

# 4) 定位输入框处吞掉了哪些异常？
grep -rn "INPUT_SELECTORS" -B4 -A12 --include=*.py . | grep -v .venv

# 5) 忙/回收判据：是否只看锁或只由锁维护的集合？
grep -rn "bucket_busy\|_bucket_busy\|_active_buckets" --include=*.py . | grep -v .venv
```

---

## 3. 回归测试怎么写（不开浏览器）

假页面是够的，关键是**假出两种「死法」**，它们对应两条不同的代码路径：

```python
class ClosedPage(HealthyPage):            # 标签被关闭：is_closed() 为真
    def is_closed(self): return True
    async def wait_for_selector(self, *a, **k): raise RuntimeError("Target page ... has been closed")

class CrashedPage(HealthyPage):           # 渲染进程崩溃：is_closed() 仍报 False，但什么都失败
    def is_closed(self): return False
    async def wait_for_selector(self, *a, **k): raise RuntimeError("Target page ... has been closed")
```

要覆盖的用例（DeepseekBridge 新增 14 例，见 `tests/test_page_recovery.py`）：

1. 定位函数在死页面上**抛专用异常**（不再返回 `None`）。
2. 页面活着但选择器全不中 → 仍然返回 `None`（别把「改版」误判成「页面死了」）。
3. `_ensure_page` 发现登记页面已死 → 重建，且**回到同一条会话 URL**、`turns` 等状态保留。
4. 默认桶页面已死 → 同样重建。
5. 页面「崩溃但 `is_closed()` 为 False」→ `send_chat` 能恢复（证明恢复路径**不依赖判活**，会无条件重建）。
6. 一直死（重建出来的也死）→ 抛出的错误文案指向「标签已失效」，且 `state.last_error` 记下真实原因。
7. 请求进行中的桶：`_recycle_idle_pages()` 不回收它、`_evict_lru_page()` 跳过它。
8. 落盘 `save_extracted_files` 按**实例**调用（`driver.save_extracted_files(a, b, c)`）可用。

---

## 4. 端到端验收（真浏览器 + 真客户端）

1. **线上复放**：用出错客户端的原样方式调用（例：OpenAI SDK 的 UA 会自动分桶，正好打在出事的桶上）。
   看**耗时**：修复前 0.1s 失败 → 修复后 ~20s 正常生成。
2. **落盘**：响应里的 `saved_files` 有路径（证明签名修复已生效——旧代码在这条必 500）。
3. **自愈（真机）**：在独立沙箱里（**别拿线上实例做实验**，`persistent context` 同一 user_data
   不能并行）用真 driver：送一轮 → `await page.close()`（等价用户关标签）→ 再送一轮，
   断言：新页面取代旧页面、`_current_session_url()` 仍是同一条会话、`turns` 继续累加。
4. **默认桶同样验一遍**：`await driver.page.close()` → 再送一轮。
5. `/healthz` 看 `browser_ready` / `open_pages` / `keys`，确认没有残留僵死桶。

沙箱配方（不碰线上）：`PORT=8010`、`USER_DATA_DIR=<user_data 的拷贝>`、
`SESSION_FILE=<状态文件拷贝>`、`TASK_FILE_DIR` / `OUTPUT_DIR` 指到 `/tmp`、
`DEEPSEEK_DEBUG=1` 拿全量日志；用完 kill 掉并删掉临时 profile（拷贝前先删 `Singleton*`）。

---

## 5. 落地映射：ChatGPTBridge（只核对过，未改动）

| 要点 | ChatGPTBridge 位置 | 现状 |
| --- | --- | --- |
| P0-K 落盘签名 | 定义 `chatgpt_web/reply_extractor.py:96`（无 `self` / 无 `@staticmethod`）；调用 `chatgpt_web/api/chat_adapter.py:427` 用 `asyncio.to_thread(driver.save_extracted_files, raw, blocks, dir)` | **缺陷在**。但它是开关式的（`config.SAVE_FILES` 默认 false，`request.save_files` 为 `None` 才回落）→ 平时潜伏，一打开就每请求 500 |
| P0-J 页面判活 | `_ensure_page`（`chatgpt_web/page_pool.py:167`）首行就是 `if bucket == DEFAULT_SESSION_KEY or bucket in self._pages: return`（第 175 行）；全项目 **grep 不到 `is_closed`** | **缺陷在**（无「页面存活」概念） |
| P0-J 定位输入框 | `chatgpt_web/browser/dom_adapter.py:49 find_input()`：逐个选择器 `except Exception` 只记字符串、最后返回 `None`；`state="attached"`（不是 `visible`，这点比 DeepseekBridge 好）；`chatgpt_web/reply_waiter.py:66-68` 拿 `None` 就抛「无法找到对话输入框，请检查 ChatGPT 网页是否打开或处于登录状态」 | **缺陷在**。好消息：它把每个选择器的异常都写进日志 → 先在日志里搜 `Target`/`closed` 就能确认是不是这条 |
| P0-J 取页时机 | `chatgpt_web/reply_waiter.py:53` 在 `async with self._session_lock(...)`（第 58 行）**之前**取 `page`，锁内**没有重取** | 建议照 DeepseekBridge 那样：进锁后 `page = self._page_for(bucket)` 重取一次 |
| P0-J 默认桶 | 同 `page_pool.py:175`（默认桶走 `self.page`，直接 `return`） | 默认桶若死 → 整桥永久 502，需一并重建 |
| P0-L 防误杀 | `bucket_busy()`（`chatgpt_web/page_pool.py:99`）只看 `_active_buckets`，而它由 `_session_lock` 在**持锁期间**维护（`page_pool.py:89/93`）；`_recycle_idle_pages`（127）/ `_evict_lru_page`（142）都用它 | **窗口在**：`_ensure_page` → 拿锁之间没有保护，需引入可重入计数 |

> 迁移时不要照抄文件结构（ChatGPTBridge 已拆出 `browser/`、`reply_waiter.py`、`reply_extractor.py`、
> `api/chat_adapter.py`），**只搬运语义**：判活、专用异常、重建入口、默认桶重建、重试阶梯接纳、
> 轮询判活、错误现场信息、落盘签名。

---

## 6. 踩过的坑（别再犯）

- **别用「节点数量」判断新回复**（这两个项目的老教训）：长会话下节点会被回收替换，数量常常恒定。
- `is_closed() == False` **不等于**页面可用：崩溃 / 挂起的渲染进程照样每次都失败。
  所以「恢复路径」必须能**无条件重建**，不能只在判活失败时才重建。
- 重建要**回到状态里的会话 URL**，否则每关一次标签就丢一次上下文、还要重放播种。
- 重试阶梯里「页面失效」这一支要**跳过轮转/换新会话**分支，否则刚恢复的会话立刻被丢掉。
- 报错文案要能自证：只说「请检查是否登录」会把「页面已经没了」这种**可自愈**故障说成**用户操作问题**，
  本次故障因此被误认了很久；把 URL / readyState / 选择器命中数 / 是否有弹层带进错误信息（不回显正文）。
- 排错时先用**耗时**分辨故障类型，再动手看选择器；反过来会白改一堆选择器。
- 测试里「类上未绑定调用」会掩盖缺少 `self` 的签名错误；公共方法至少补一条**按实例调用**的用例。
