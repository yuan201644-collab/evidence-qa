"""tokenizer.py - RAG 检索用的统一分词器

解决什么问题（对应 retriever 之前字符级方案的三个缺陷）：
    1) 词边界丢失：单字切会把「质保期」拆成 质/保/期，误召回变多。
       改用 jieba 分词后，「质保期」作为整词，精度提升。
    2) 长度归一化失真：中文按字数算文档长度，和英文词数不在同一尺度，
       导致 BM25 的 b 参数归一化错位。jieba 把中文降到词尺度，缓解该问题。
    3) 专有名词命中：jieba 自带词典，能把「质保期」这类中文术语保持为整词，
       查询命中率提升。（注意：BM25 不关心词位置，子串/近似匹配仍需 bge 语义向量）

设计：
    - 优先用 jieba 做中文分词；若 jieba 未安装，退回字符级（保证无依赖环境仍可跑）。
    - 非中文（英文/数字型号、编号）按连续字母数字整体保留，如 DeepSeek、V3。
    - 内置轻量中文停用词，过滤极高频率、无检索区分度的虚词，直接压低问题一的噪声。
"""

from pathlib import Path
import re
# 停用词来源：app/rag/resources/stopwords.txt（外置文件，团队可共用、评审可见、改词不动代码）。

_DATA_STOPWORDS_PATH = Path(__file__).parent / "resources" / "stopwords.txt"


def load_stopwords(path: Path) -> set:
    """从停用词文件读入一个 set。

    每行一个词；空行忽略。
    """
    stopwords = set()

    with open(path, encoding="utf-8") as f:
        for line in f:
            word = line.strip()
            if word:
                stopwords.add(word)
    return stopwords


_STOPWORDS = load_stopwords(_DATA_STOPWORDS_PATH)


def _is_cjk(ch):
    """判断一个字符是不是中日韩统一表意文字（常用汉字区间）。
    这个函数用来区分“这个字符是不是中文”，以便决定走 jieba 还是走字母数字提取
    ord(ch) 拿到字符的 Unicode 码点。0x4E00 到 0x9FFF 是中日韩统一表意文字（常用汉字）的区间"""
    code = ord(ch)
    if 0x4E00 <= code <= 0x9FFF:
        return True

    return False


def _split_cjk_and_non(text):
    """把文本拆成「连续中文段」和「连续非中文段」的交替列表。

    返回 [(片段, 是否中文), ...]。这样中文走 jieba、非中文走字母数字整体提取，
    互不干扰。
    """
    result = []
    buf = ""
    buf_is_cjk = None
    for ch in text:
        is_cjk = _is_cjk(ch)
        if buf_is_cjk is None:
            buf = ch
            buf_is_cjk = is_cjk
        elif is_cjk == buf_is_cjk:
            buf = buf + ch
        else:
            result.append((buf, buf_is_cjk))
            buf = ch
            buf_is_cjk = is_cjk
#维护一个缓冲区 buf 和它的类型 buf_is_cjk。
#遍历每个字符，如果当前字符和缓冲区类型一致，就追加；不一致就切段、开新段。
#最后把缓冲区里剩下的也输出
    if buf != "":
        result.append((buf, buf_is_cjk))   #把剩下没加入的字符也加入result
    return result
#把 "设备ABC-2026质保期" 拆成： [("设备", True), ("ABC-2026", False), ("质保期", True)] 是中文就是对应字段，True


def _cjk_words(seg):
    """对一段纯中文做分词，返回词列表。"""

    try:
        import jieba
        return jieba.lcut(seg)
    except ImportError:
            # jieba 不可用，退回字符级
        pass
    # 字符级兜底
    words = []
    for ch in seg:
        words.append(ch)
    return words


def tokenize(text):
    """把一段文本切成检索用的 token 列表。

    中文段优先 jieba 分词（保留词边界），jieba 不可用时退回字符级；
    非中文段按连续字母/数字整体作为 token，保留型号与编号。
    过滤空白、停用词与单字噪声。
    """
    tokens = []
    segments = _split_cjk_and_non(text)
    for seg, is_cjk in segments:     #("设备", True), ("ABC", False) 元组形式
        if is_cjk:
            words = _cjk_words(seg)    #_split_cjk_and_non只是把中文和其他字符分开  _cjk_words才是把对应的中文连续字段按词拆分
            for w in words:
                if w == "":  #跳过空
                    continue
                if w in _STOPWORDS:  #跳过停用词
                    continue
                tokens.append(w)
        else:
            # 非中文：多粒度 token —— 整体保留 + 拆分
            # 第一步：按非字母数字切分，得到拆分子 token

            parts=[]
            for part in re.split(r"[^A-Za-z0-9]+",seg):
                part=part.lower()  #统一换为小写 不然用户输入小写会匹配不上数据库的大写内容
                if part =="":
                    continue
                if part in _STOPWORDS:
                    continue
                parts.append(part)

            # 第二步：去掉首尾非字母数字后，整段作为一个整体 token
            # 只去首尾，保留中间的 - . / 等，型号不被变形
            # 整体也要 lower，否则 "ABC" 和 "abc" 会被当成两个 token
            whole = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", seg).lower()

            # 第三步：先放整体，再放拆分结果
            # 如果整体和拆分结果一样（比如 "abc"），就不重复加
            # 整体若含空白（例如 "ABC-2026 xyz" 被整段当成一个非中文段），
            # 视为退化 token 跳过——parts 已经把它按符号/空格拆开覆盖检索。
            seen = set()
            if whole != "" and not re.search(r"\s", whole):
                if whole not in _STOPWORDS:
                    tokens.append(whole)
                    seen.add(whole)
            for part in parts:
                if part not in seen:
                    tokens.append(part)
                    seen.add(part)

    return tokens

