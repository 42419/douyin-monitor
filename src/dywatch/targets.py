"""通知目标的配置语法 —— 一个渠道类型可以有**多个实例**。

    NOTIFY_TARGETS="
    dingtalk name=市场部 token=xxx secret=SECyyy
    dingtalk token=zzz
    # 想临时停掉某个渠道就注释掉这一行
    telegram bot_token=1:AA chat_id=-100
    "

`.env` 支持带引号的多行值（python-dotenv 认），所以**一行一个目标**——读起来跟 `users.conf`
（一行一个账号）是一个路子：好读、好改、能注释掉。

## 语法

- **一行一个目标**（也接受用 `;` 分隔多个目标，方便写成一行）
- 每行第一个词是**类型**，其余是 `key=value`，字段之间用**空白或逗号**分隔
  （`dingtalk name=市场部,token=xxx` 与空格版等价，混着写也行）。类型后面可以跟一个 `:`：
  `dingtalk:token=xxx`、`dingtalk: token=xxx`、`dingtalk token=xxx` 都认
- `key=value` 只按**第一个** `=` 切分，所以值里可以带 `=`（base64、Telegram 的 `123:AA`）
- `#` 前面有空白（或在行首）才是注释，直到行尾；想**停用**某个渠道就把它注释掉
- 值里要出现空白、逗号或 `#`、`;`，就给它加引号：`url='https://x/a,b#frag'`。
  **注意外层已经是 `"`**（多行值的引号），所以内层要用 `'`——同一种引号会让 `.env`
  解析器读不出整个键（`settings._dropped_env_keys` 会把它变成启动错误）
  不加引号而值里出现 `#` 会**报错**（而不是猜你想注释还是想当值）——这个坑踩过：
  `secret=SECx#备注` 会被当成完整密钥，加签失败、通知静默发不出去
- 值里如果本身要出现引号字符（比如英文名带撇号 `name=O'Brien`），也要整体加**另一种**
  引号包起来：`name="O'Brien"`。裸着写一个 `'` 或 `"` 解析器会当成引号的开始，把后面
  一段（可能连着下一行）都当成这个值的一部分，直到遇到下一个同类型的引号字符才闭合——
  轻则报一个跟真实错误位置对不上的提示，重则把下一行本该独立的目标悄悄吞掉一部分字段。
  这个解析器目前不会替你从字面意图猜出"这是不是一个真的引号"，所以裸引号字符一律先加
  引号包起来最省心
- 同一行里同一个字段写两次（`token=A token=B`）是**错误**，不静默取最后一个

## 实例名

投递结果（`Delivery.sent` / `failed`）与只读面板都会显示实例名，所以**它必须全局唯一**：
`failed` 是字典，两个实例同名会互相覆盖失败原因。

- 不写 `name=` 时按类型自动编号：第一个叫 `dingtalk`，第二个 `dingtalk-2`，第三个 `dingtalk-3`…
- `name=市场部` 可以用可读别名（同样要全局唯一）

## 与旧写法的关系

旧写法（`NOTIFY_CHANNELS=dingtalk` + `DINGTALK_TOKEN` 这类**单值键**）**继续可用**，但不支持多实例。
两种同时配时**新写法生效**，旧的那几项被忽略（`settings.warnings()` 会说明这一点）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping

#: 目标之间的分隔符（行是主要写法，`;` 是写成一行的便利写法）
ENTRY_SEP: Final[str] = ";"
#: 字段之间的分隔符（空白最常见的那个单独列出来是为了文档；逗号是同等的）
FIELD_SEP: Final[str] = " "
FIELD_SEP_ALT: Final[str] = ","
#: 注释起始符（只在一个字段的**开头**生效）
COMMENT: Final[str] = "#"
#: 值里要带空白/逗号时用的引号
QUOTES: Final[str] = "'\""

#: 写错时的通用提示：给出正确的形状，而不是只说"不认识"
SYNTAX_HINT: Final[str] = (
    "一行一个目标、字段用空格或逗号分隔，例如 `dingtalk name=市场部, token=xxx, secret=SECyyy`"
)


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """一个渠道类型允许哪些字段。"""

    #: 必填字段（缺一个就不产出这个目标）
    required: tuple[str, ...]
    #: 选填字段（不写就用渠道自己的默认值）
    optional: tuple[str, ...] = ()
    #: 属于凭据的字段：`dywatch config-check` 与日志里要遮蔽
    secrets: tuple[str, ...] = ()


#: 类型 -> 字段表。这里的键名**刻意用渠道自己的说法**（token / key / sendkey），
#: 而不是大写环境变量名——它出现在一行配置里，短一点更好写、也更好读。
SPECS: Final[Mapping[str, TargetSpec]] = {
    "dingtalk": TargetSpec(("token",), ("secret",), secrets=("token", "secret")),
    "wecom": TargetSpec(("key",), secrets=("key",)),
    "bark": TargetSpec(("device_key",), ("server",), secrets=("device_key",)),
    "serverchan": TargetSpec(("sendkey",), secrets=("sendkey",)),
    "telegram": TargetSpec(("bot_token", "chat_id"), secrets=("bot_token",)),
    # webhook 的 URL 本身可能就是带密钥的回调地址，一并遮蔽
    "webhook": TargetSpec(("url",), secrets=("url",)),
}

#: `name` 是所有类型通用的元字段（不是渠道参数）
NAME_FIELD: Final[str] = "name"


@dataclass(frozen=True, slots=True)
class Target:
    """一个渠道实例：`dingtalk:市场部(token=…)`。"""

    kind: str
    name: str
    fields: Mapping[str, str]

    def masked(self) -> str:
        """给 `config-check` 看的形态：凭据打码，其余照实。"""
        secrets = SPECS[self.kind].secrets
        parts = [
            f"{key}={'***' if key in secrets and value else (value or '(空)')}"
            for key, value in sorted(self.fields.items())
        ]
        return f"{self.kind}:{self.name}({FIELD_SEP.join(parts)})"


@dataclass(frozen=True, slots=True)
class TargetSet:
    """解析结果：能用的目标 + 解析过程中发现的问题。

    `configured` 与 `targets` 是两件事：配了但全写错时，前者为真、后者为空——
    此时**不该**回落到旧写法（否则用户以为新写法在跑，实际跑的是旧的另一套凭据）。
    解析错误会被 `Settings.validate()` 报出来，进程根本不会启动到发通知那一步。
    """

    targets: tuple[Target, ...] = ()
    errors: tuple[str, ...] = ()
    configured: bool = False

    def __bool__(self) -> bool:
        """`if settings["NOTIFY_TARGETS"]:` == "有能发的目标"。"""
        return bool(self.targets)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(target.name for target in self.targets)


def parse_targets(raw: Any) -> TargetSet:
    """把配置值解析成目标集合。**任何问题都收进 `errors`**，不抛异常。

    不抛异常是刻意的：`_cast()` 抛出去会被配置加载器当成"这个值坏了"而退回默认值——那样
    用户看到的是"目标全没了"，而不是"第 2 个目标的 secret 拼错了"。
    """
    text = _raw_text(raw)
    if not text:
        return TargetSet()

    targets: list[Target] = []
    errors: list[str] = []
    seen_names: set[str] = set()
    per_kind: dict[str, int] = {}

    for entry in _entries(_strip_comments(text)):
        kind, fields, field_errors = _parse_entry(entry)
        if field_errors:
            # 字段有问题就整条丢掉：留着"半个目标"会带着被截断的值跑
            # （例如 `name=a,b` 里的名字会被逗号切成 `a`）。
            # 类型/引号级别的问题本身已经说清了，这里只为"字段级"错误补一句"这条丢了"。
            errors.extend(field_errors)
            if kind is not None:
                errors.append(f"（{kind} 这一条已忽略：上面的字段问题要先改掉）")
            continue
        if kind is None:
            continue
        spec = SPECS[kind]
        missing = [key for key in spec.required if not fields.get(key)]
        if missing:
            errors.append(f"{kind} 缺少必填字段：{'、'.join(missing)}")
            continue

        if NAME_FIELD in fields and not fields[NAME_FIELD]:
            errors.append(f"{kind} 的 name= 是空的——要么删掉它，要么给个名字")
            continue

        per_kind[kind] = per_kind.get(kind, 0) + 1
        # 编号用 `-` 不用 `#`：`#` 在值里是注释起始符（要写就得加引号），
        # 拿它当编号会让人抄不出来
        default_name = kind if per_kind[kind] == 1 else f"{kind}-{per_kind[kind]}"
        name = fields.pop(NAME_FIELD, "") or default_name
        if name in seen_names:
            errors.append(f"实例名 {name!r} 重复——名字会进投递结果与面板，必须全局唯一")
            continue
        seen_names.add(name)
        targets.append(Target(kind=kind, name=name, fields=fields))

    return TargetSet(tuple(targets), tuple(errors), configured=True)


def _raw_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (list, tuple)):
        return "\n".join(str(item).strip() for item in raw if str(item).strip())
    return ""


def _strip_comments(text: str) -> str:
    """先把注释去掉，**再**交给 `_entries` 分条。

    注释必须在分条**之前**处理，原因是分条同时要做引号配对：注释里有一个落单的引号
    （`# can't disable telegram` 里的撇号）时，分条会以为引号一直没闭合，把后面的目标
    整段吞进同一个条目，`_tokenize` 又在条目开头的 `#` 处 `break`——于是那个渠道
    **无声无息地消失**，`errors` 里什么都没有。反过来，注释里出现 `;` 时，分条会把它
    当成目标分隔符，一个被注释掉的渠道会被**重新激活**（`# backend;telegram ...`）。
    两条路都是"安静地做错事"，所以注释的判定只用一条规则、只在一处发生。

    注释规则与 `_tokenize` 保持一致（也保持文档的承诺）：

    * 行首或**前面是空白/逗号/`;`** 的 `#` → 注释到行尾；
    * 引号里的 `#` 是值的一部分（`url='https://x/a#frag'`），不动；
    * 其它位置的裸 `#`（`secret=SECx#备注`）**不在这里处理**，留给 `_tokenize` 报错——
      猜"这是注释还是值"正是当初静默发不出通知的原因，这里不重复那个错误。
    """
    out: list[str] = []
    for line in text.splitlines():
        quote: str | None = None
        current: list[str] = []
        for char in line:
            if quote is not None:
                current.append(char)
                if char == quote:
                    quote = None
                continue
            if char in QUOTES:
                quote = char
                current.append(char)
                continue
            if char == COMMENT and (
                not current or current[-1].isspace() or current[-1] in (FIELD_SEP_ALT, ENTRY_SEP)
            ):
                break
            current.append(char)
        out.append("".join(current))
    return "\n".join(out)


def _entries(text: str) -> list[str]:
    """拆成"一行一个目标"。空行丢掉；**引号外**的 `;` 也算目标分隔符（写成一行的便利写法）。

    必须自己做，不能先 `replace(';', '\n')`：那样引号就保护不了值里的 `;`，
    而文档明确承诺"值里要带 `;` 就加引号"（独立审查抓到的矛盾）。

    调用方要先过一遍 `_strip_comments`（见那里的理由：注释里一个撇号就能让引号配不上对）。
    """
    out: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in text:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in QUOTES:
            quote = char
            current.append(char)
            continue
        if char == ENTRY_SEP or char == "\n":
            piece = "".join(current).strip()
            if piece:
                out.append(piece)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        out.append(tail)
    return out


def _tokenize(entry: str) -> tuple[list[str], str | None]:
    """把一行拆成字段。空白与逗号都算分隔符；引号里的内容原样保留（连同分隔符）。

    返回 (字段列表, 错误)。带引号是为了留两个出口：值里要带分隔符（`url="https://x/a,b"`），
    或者值里要带 `#`。
    """
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    closed_at: int | None = None  # 最近一次闭合引号的位置，用来判断"引号后面还粘着内容"
    for index, char in enumerate(entry):
        if quote is not None:
            if char == quote:
                quote = None
                closed_at = index
            else:
                current.append(char)
            continue
        if char in QUOTES:
            quote = char
            continue
        if closed_at is not None and closed_at == index - 1 and char not in (FIELD_SEP, FIELD_SEP_ALT) and not char.isspace():
            # `url="https://x"GARBAGE`：不报错的话会静默拼成 `https://xGARBAGE`
            return tokens, (
                f"引号后面的 {char!r} 没有分隔：引号要包住**整个值**，"
                '例如 url="https://x/a,b"（不是 url="https://x"x）'
            )
        if char == COMMENT:
            if not current:
                break  # 字段开头 → 注释，直到行尾
            # 值里出现没加引号的 `#`：猜不出你是想注释还是想把它当值，所以**报错**
            return tokens, (
                "值里出现了没加引号的 `#`：想写注释就在它前面留一个空格；"
                '值里确实要 `#`（比如 URL 的片段）就加引号，例如 url="https://x/a#frag"'
            )
        if char in (FIELD_SEP, FIELD_SEP_ALT) or char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if quote is not None:
        return tokens, "引号没有闭合（值里带空白/逗号/`#` 时才需要引号）"
    if current:
        tokens.append("".join(current))
    return tokens, None


def _parse_entry(entry: str) -> tuple[str | None, dict[str, str], list[str]]:
    """解析一行：第一个词是类型，其余是 `key=value`。返回 (类型, 字段, 错误)。"""
    tokens, tokenize_error = _tokenize(entry)
    if tokenize_error:
        return None, {}, [tokenize_error]
    if not tokens:
        return None, {}, []

    # 类型允许写成 `dingtalk` / `dingtalk:` / `dingtalk:token=x`——只在这里切一次，
    # 后面几个 token 里的 `:` 属于值（Telegram 的 `bot_token=123:AA` 就是靠这个保住）
    head, colon, attached = tokens.pop(0).partition(":")
    if colon and attached:
        tokens.insert(0, attached)
    kind = head.lower()
    if kind not in SPECS:
        # 类型位置写的是"一整段配置"（`token=x,secret=y`、没写类型）时，顺手把正确形状带上，
        # 而不是只回一句"不认识这个类型"
        hint = f"（{SYNTAX_HINT}）" if any(ch in kind for ch in ":,=") else ""
        return None, {}, [f"未知渠道类型 {kind!r}；可用：{' / '.join(sorted(SPECS))}{hint}"]

    allowed = set(SPECS[kind].required) | set(SPECS[kind].optional) | {NAME_FIELD}
    fields: dict[str, str] = {}
    errors: list[str] = []
    # 注意是 `tokens` 而不是 `tokens[1:]`：类型那个 token 上面已经 pop 掉了，
    # （`类型:字段=值` 的情况）紧贴的类型字段也已经插回开头
    for token in tokens:
        key, has_eq, value = token.partition("=")
        key = key.strip().lower()
        if not has_eq:
            errors.append(f"{kind} 的 {token!r} 不是 key=value 形式（{SYNTAX_HINT}）")
            continue
        if key not in allowed:
            errors.append(f"{kind} 不认识字段 {key!r}；可用：{' / '.join(sorted(allowed))}")
            continue
        if key in fields:
            errors.append(f"{kind} 的字段 {key!r} 写了两遍——不静默取最后一个，请删掉一个")
            continue
        fields[key] = value.strip()
    return kind, fields, errors


__all__ = [
    "COMMENT",
    "ENTRY_SEP",
    "FIELD_SEP",
    "FIELD_SEP_ALT",
    "QUOTES",
    "NAME_FIELD",
    "SPECS",
    "SYNTAX_HINT",
    "Target",
    "TargetSet",
    "TargetSpec",
    "parse_targets",
]
