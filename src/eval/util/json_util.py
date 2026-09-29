import json
import re
from typing import Dict, List, Any, Union, Optional

def is_json(data: str) -> bool:
    try:
        json.loads(data)
        return True
    except ValueError:
        return False

def text_to_json_parser(raw_text: str) -> Optional[Dict[str, Any]]:
    """
    文本转JSON解析器：处理带转义符、换行符、特殊字符的JSON格式文本
    :param raw_text: 原始JSON格式文本（含转义符、换行符等）
    :return: 解析后的标准JSON字典，解析失败返回None
    """
    if not raw_text or not isinstance(raw_text, str):
        print("错误：输入为空或非字符串类型")
        return None

    try:
        # 步骤1：预处理文本 - 清理无效字符、修复转义
        processed_text = raw_text.strip()

        # 移除文本首尾可能的多余引号（若有）
        if processed_text.startswith('"') and processed_text.endswith('"'):
            processed_text = processed_text[1:-1]

        # 处理转义符：还原JSON标准转义（重点处理\\n、\\"等）
        # 先将\\\\n还原为\\n，再处理其他转义
        processed_text = re.sub(r'\\\\n', r'\n', processed_text)
        processed_text = re.sub(r'\\"', r'"', processed_text)
        processed_text = re.sub(r'\\\\', r'\\', processed_text)

        # 步骤2：解析为JSON对象
        json_data = json.loads(processed_text)

        # 步骤3：验证解析结果（确保是字典类型）
        if not isinstance(json_data, dict):
            print("警告：解析结果非JSON对象（dict），返回None")
            return None

        return json_data

    except json.JSONDecodeError as e:
        # 详细错误提示，定位解析失败位置
        error_msg = f"JSON解析失败：{e.msg}（行：{e.lineno}，列：{e.colno}）"
        print(error_msg)

        # 尝试修复常见问题后重试（如未闭合的引号、多余逗号）
        try:
            # 修复多余逗号（如 [1,2,] → [1,2]）
            fixed_text = re.sub(r',\s*}', '}', processed_text)
            fixed_text = re.sub(r',\s*]', ']', fixed_text)
            json_data = json.loads(fixed_text)
            print("提示：修复多余逗号后解析成功")
            return json_data
        except:
            print("重试解析失败，返回None")
            return None
    except Exception as e:
        print(f"未知错误：{str(e)}")
        return None


def format_json_output(json_data: Dict[str, Any], indent: int = 2) -> str:
    """
    格式化JSON数据为易读字符串
    :param json_data: 解析后的JSON字典
    :param indent: 缩进空格数
    :return: 格式化后的字符串
    """
    if not json_data:
        return ""
    try:
        return json.dumps(json_data, ensure_ascii=False, indent=indent)
    except:
        return str(json_data)


def extract_json_valid_data(
        json_data: Union[str, Dict, List],
        parent_key: str = "",
        sep: str = ".",
        ignore_empty: bool = True
) -> List[Dict[str, Any]]:
    """
    解析 JSON 数据，提取所有有数据的参数名（键）和数值
    :param json_data: JSON 字符串 / JSON 对象（dict）/ JSON 数组（list）
    :param parent_key: 父级键名（递归用，外部调用无需传）
    :param sep: 嵌套键的分隔符（如 a.b.c）
    :param ignore_empty: 是否忽略空值（空字符串、空数组、空对象、null）
    :return: 结构化结果列表，每个元素包含 "param_name"（参数名）和 "value"（数值）
    """
    # 存储最终提取的键值对
    result = []

    # 第一步：处理 JSON 字符串 → 转换为 dict/list
    if isinstance(json_data, str):
        try:
            json_data = json.loads(json_data)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 解析失败：{e}") from e

    # 第二步：递归处理 JSON 对象（dict）
    if isinstance(json_data, dict):
        for key, value in json_data.items():
            # 拼接嵌套键名（如 parent.key）
            current_key = f"{parent_key}{sep}{key}" if parent_key else key

            # 递归处理嵌套对象/数组
            if isinstance(value, (dict, list)):
                result.extend(extract_json_valid_data(value, current_key, sep, ignore_empty))
            else:
                # 判断是否为空值（需过滤的情况）
                is_empty = False
                if ignore_empty:
                    if value is None:
                        is_empty = True
                    elif isinstance(value, str) and value.strip() == "":
                        is_empty = True

                # 非空值才加入结果
                if not is_empty:
                    result.append({
                        "param_name": current_key,
                        "value": value
                    })

    # 第三步：处理 JSON 数组（list）
    elif isinstance(json_data, list):
        for idx, item in enumerate(json_data):
            # 数组元素的键名拼接（如 parent[0]）
            current_key = f"{parent_key}[{idx}]" if parent_key else f"[{idx}]"

            # 递归处理数组内的对象/数组/值
            if isinstance(item, (dict, list)):
                result.extend(extract_json_valid_data(item, current_key, sep, ignore_empty))
            else:
                # 判断是否为空值
                is_empty = False
                if ignore_empty:
                    if item is None:
                        is_empty = True
                    elif isinstance(item, str) and item.strip() == "":
                        is_empty = True

                if not is_empty:
                    result.append({
                        "param_name": current_key,
                        "value": item
                    })

    return result


