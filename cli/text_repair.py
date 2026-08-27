"""修复流式文本里落单的代理字符（lone surrogate）。

网关会把一个字符拆到两个 SSE chunk 里。两种拆法都见过：

- **UTF-16 代理对被拆开**：`💥` 是 `\\ud83d\\udca5`，两半分到相邻两个 delta。
- **UTF-8 字节被拆开**：`股` 是 `e8 82 a1`，落单的字节以 surrogateescape
  的形式（`\\udce8` 这类）到达。

落单的代理字符没法用 strict UTF-8 编码。而下游全是 strict：IPC 协议通道写盘、
chat_log 落 SQLite、scratchpad 落 JSONL、远程遥控算帧长 —— 任何一处抛
UnicodeEncodeError，整轮回答都被 run_turn 的兜底 except 吞掉，界面上只剩一行
`'utf-8' codec can't encode character ...`。

所以在这里做两件事：把拆开的两半重新接上，接不上的换成 U+FFFD。
"""

from __future__ import annotations

# 末尾攥住多少个代理字符等下文来拼。一个字符最多占 4 个 UTF-8 字节，UTF-16
# 代理对占 2 个码位，所以 4 个足够拼出任何被拆开的字符；再多就不可能是「半个
# 字符」了，只能是连续的多个字符，得放行，否则一直攥着不发。
_MAX_PENDING = 4

_SURROGATE_LOW = 0xD800
_SURROGATE_HIGH = 0xDFFF


def repair_text(text: str) -> str:
    """把落单的代理字符换成 U+FFFD；能配对的先合起来。"""
    if not text:
        return text
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        pass
    try:
        # 先按「UTF-8 字节被拆开」解 —— surrogateescape 的逆运算能还原出原字符。
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:
        # 掺了 UTF-16 代理对（表情符号被拆半），surrogateescape 只认 \\udc80-\\udcff。
        # surrogatepass 能把配对的合起来，真落单的交给 replace。
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _split_pending(text: str) -> tuple[str, str]:
    """切出末尾那段可能是「半个字符」的代理字符，留到下一块再拼。"""
    cut = len(text)
    while cut > 0 and _SURROGATE_LOW <= ord(text[cut - 1]) <= _SURROGATE_HIGH:
        cut -= 1
    return (text[:cut], text[cut:]) if len(text) - cut <= _MAX_PENDING else (text, "")


class StreamTextRepair:
    """逐块修复流式文本，末尾的半个字符攥到下一块拼上。

    一路 feed，流结束时 flush 一次把攥着的尾巴放出来 —— 不 flush 的话，恰好
    结束在半个字符上的那点内容会丢。
    """

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> str:
        body, self._pending = _split_pending(self._pending + text)
        return repair_text(body)

    def flush(self) -> str:
        tail, self._pending = self._pending, ""
        return repair_text(tail)
