import json
import re

from agents.verify_rule_base_by_verify_function.scribe_accessor import headers
from util.base64_util import is_base64
from util.binary_util import is_binary_data
from util.graphql_util import parse_graphql_query
from util.hex_util import is_hex
from util.json_util import is_json
from util.url_util import url_decode

EXCLUDE_FIELD = ["browser","userAgent","sec-ch-ua","sec-ch-ua-platform","cookie","application","device","source","identities","appState","content","event","events","session","eventUuid","interaction","experiments","deviceAndBrowser","token"]
GRAPHQL_FIELDS = ["query","graphql"]

# def format_graphql_query(raw_query: str) -> str:
#     """
#     压缩GraphQL Query：移除冗余换行/空格，仅保留单个分隔符（优先保留\n）
#     :param raw_query: 原始GraphQL Query字符串
#     :return: 压缩后的Query字符串
#     """
#     if not isinstance(raw_query, str) or raw_query.strip() == "":
#         return ""
#
#     # 步骤1：替换制表符为换行（统一空白类型）
#     compressed = raw_query.replace("\t", "\n")
#
#     # 步骤2：拆分语法关键字符（避免粘连），包裹换行
#     # 匹配 { } ( ) : , ! @ = ~ > < & | # % ^ [ ] 等GraphQL语法字符
#     syntax_pattern = re.compile(r"([{}():,!@=~><&|#%^\[\]])")
#     compressed = syntax_pattern.sub(r"\n\1\n", compressed)
#
#     # 步骤3：将连续的空白（\n/空格）替换为单个\n（优先换行）
#     whitespace_pattern = re.compile(r"[\n\s]+")
#     compressed = whitespace_pattern.sub("\n", compressed)
#
#     # 步骤4：修复语法关键字符周围的冗余换行
#     # 移除 {/(/[ 后的换行、}/)/] 前的换行
#     compressed = re.sub(r"([{(\[])\n", r"\1", compressed)
#     compressed = re.sub(r"\n([})\]])", r"\1", compressed)
#     # 移除 ,/: 后的换行、!/?/@ 前的换行（语法必需的紧凑格式）
#     compressed = re.sub(r"([,:])\n", r"\1", compressed)
#     compressed = re.sub(r"\n([!@?])", r"\1", compressed)
#     # 保留 query/mutation/fragment 关键字后的单个空格（避免粘连）
#     compressed = re.sub(r"\n(query|mutation|fragment)\n", r"\n\1 ", compressed)
#     # 保留 on 关键字前后的单个空格（如 fragment X on Product → 避免XonProduct）
#     compressed = re.sub(r"\n(on)\n", r" \1 ", compressed)
#
#     # 步骤5：修剪首尾空白，合并连续换行（最终仅保留单个\n）
#     compressed = compressed.strip()
#     compressed = re.sub(r"\n+", "\n", compressed)
#
#     return compressed

def format_str(data: str):

    # url 字符串
    if data.startswith('http'):
        data = url_decode(data)

    # json 字符串
    if is_json(data):
        return format_data(json.loads(data))


    # binary 字符串
    if is_binary_data(data) == "binary data":
        return "binary data"

    # base64 字符串
    if is_base64(data):
        return "base64 data"

    # Hex 字符串
    if is_hex(data):
        return "hex data"

    # 普通字符串
    return data
def format_data(data: dict):
    # 格式化数据，去掉换行符和空格
    if isinstance(data, dict):
        keys_to_delete = []
        for key, value in data.items():

            if key in EXCLUDE_FIELD:
                keys_to_delete.append(key)
                continue

            if not value:
                keys_to_delete.append(key)
                continue


            if key in GRAPHQL_FIELDS:
                data[key]=parse_graphql_query(value)
                continue

            data[key] = format_data(value)

            if not value:
                keys_to_delete.append(key)

        for key in keys_to_delete:
            del data[key]
    elif isinstance(data, str):
        return format_str(data)
    elif isinstance(data, list):
        for i in range(len(data)):
            data[i] = format_data(data[i])
    return data

def requests_formatter(requests):
    # 格式化请求
    for request in requests:
        if "headers" in request:
            del request["headers"]

        # 2. 先检查键存在，再判断值非空（避免 KeyError）
        if "post_data" in request:
            del request["post_data"]

        # 3. 同理处理 raw_post_data
        if "raw_post_data" in request:
            del request["raw_post_data"]

        format_data(request)

    return requests