import re
from typing import List, Dict, Any, Set
from urllib.parse import urlparse, parse_qs, unquote

from util.field_name_util import parse_camel_to_snake
from util.semantics_util import get_semantic_similarity
from util.word_util import check_single_word

IGNORED_FIELDS = ["url", "method", "resource_type", "timestamp"]


# ------------------------------ 4. 文本处理函数 ------------------------------
def remove_unwanted_symbols(text: str) -> str:
    """
    去除文本中的无意义符号，保留业务核心字符
    :param text: 原始提取的文本（含GraphQL符号、特殊字符）
    :return: 清洗后的纯文本（仅保留字母、数字、-_、单个空格）
    """
    if not isinstance(text, str):
        return ""

    # 步骤1：移除GraphQL语法符号、特殊字符（逐个匹配替换为空）
    # 需去除的符号列表，可根据实际情况补充
    symbols_to_remove = r'[{}$!@():\n\t;,.]'
    text = re.sub(symbols_to_remove, '', text)

    # 步骤2：合并多个空格为单个空格（避免冗余空格）
    text = re.sub(r'\s+', ' ', text)

    # 步骤3：去除首尾空格（可选）
    text = text.strip()

    return text


def extract_words_from_url(url: str) -> Set[str]:
    """URL词汇提取（不提取域名/主机名相关词汇）"""
    decoded_url = unquote(url)
    parsed = urlparse(decoded_url)
    words = set()
    stopwords = {'com', 'org', 'net', 'www', 'http', 'https', 'html', 'php', 'jsp', 'api', 'v1', 'v2', 'gateway',
                 'graphql'}

    # 移除主机名（domain）提取逻辑 ↓ 已删除原主机名处理代码

    # 路径（保留原有逻辑）
    for part in parsed.path.split('/'):
        if part and part not in stopwords and not part.isdigit():
            wp = check_single_word(part)
            if wp:
                for w in wp:
                    words.add(w)

    # 查询参数（保留原有逻辑）
    for key, values in parse_qs(parsed.query).items():
        key = key.lower()
        if key not in stopwords:
            words.add(key)
            for value in values:
                if value and not value.isdigit() and value.lower() not in stopwords:
                    key = remove_unwanted_symbols(key)
                    value = remove_unwanted_symbols(value)
                    kl = check_single_word(key)
                    key = ""
                    if kl:
                        for w in kl:
                            key += w+" "
                    vl = check_single_word(value)
                    value = ""
                    if vl:
                        for w in vl:
                            value +=w+" "
                    if key == "" and value == "":
                        continue
                    else:
                        words.add(f"{key} {value}")
                        words.add(value.lower())
    return words


def extract_words_from_json(key, data):
    kl = check_single_word(key)
    key = ""
    for w in kl:
        key += w+" "
    result = set()
    if isinstance(data, str):
        remove_unwanted_symbols(data)
        vl = check_single_word(data)
        data = ""
        for w in vl:
            data += w+" "
        result.add(key + " " + data)
    elif isinstance(data, dict):
        for k, v in data.items():
            result.update(extract_words_from_json(k, v))
    elif isinstance(data, list):
        for item in data:
            result.update(extract_words_from_json(key, item))
    return result


def extract_words_from_request(request: Dict) -> Set[str]:
    key_value_pairs = extract_words_from_url(request.get("url", ""))
    for key, value in request.items():
        if key in IGNORED_FIELDS:
            continue
        key_value_pairs.update(extract_words_from_json(key, value))
    return key_value_pairs


def build_request_text(request: Dict[str, Any]) -> str:
    """构建请求文本"""
    words = extract_words_from_request(request)
    parse_words = set()
    for word in words:
        parse_words.add(parse_camel_to_snake(word))
    result_text = ""
    for word in parse_words:
        result_text += word + " "

    return result_text


# ------------------------------ 5. 核心过滤函数 ------------------------------
def nlp_filter_requests(
        requests: List[Dict[Any, Any]],
        keywords: List[str],
        similarity_threshold: float = 0.6
) -> List[Dict[Any, Any]]:
    """核心过滤函数"""
    if not requests or not keywords:
        return []

    # 构建关键词文本
    keywords_text = " ".join(keywords)
    filtered = []

    # 处理每个请求
    for req in requests:
        request_text = build_request_text(req)

        if not request_text:
            continue

        # 计算相似度
        similarity = get_semantic_similarity(request_text, keywords_text)

        if similarity >= similarity_threshold:
            req["_similarity"] = similarity
            filtered.append(req)

    return filtered
