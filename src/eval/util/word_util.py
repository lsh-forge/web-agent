from typing import List

import requests

# 默认本地API地址
BASE_URL = "http://localhost:12345"


def check_single_word(string: str) -> list:
    """调用本地API检测单个单词是否有语义"""
    resp = requests.get(f"{BASE_URL}/check", params={"string": string}, timeout=10)
    return resp.json().get("words")


# 2. 完善批量调用函数
def check_words_batch(strings: List[str]) -> List[str]:
    """
    调用批量语义检测接口，获取有效单词列表

    Args:
        strings: 待检测的字符串列表

    Returns:
        BatchStringCheckResponse: 包含是否有语义和有效单词列表的响应对象

    Raises:
        requests.exceptions.RequestException: 网络请求异常
        ValueError: 接口响应格式错误
    """
    # 接口地址
    url = f"{BASE_URL}/check_batch"

    # 发送 POST 请求（根据接口设计选择 params 或 json）

    # 方案2：适合长列表（推荐，避免 URL 过长）
    resp = requests.post(
        url,
        json={"strings": strings},  # 用 json 传递列表
        timeout=10,
        headers={"Content-Type": "application/json"}
    )

    # 检查响应状态码
    resp.raise_for_status()

    # 解析响应（用 Pydantic 验证格式，确保与模型一致）
    response_data = resp.json()
    return response_data.get("words")


# 3. 辅助函数：快速判断是否有语义（可选）
def has_meaning_batch(strings: List[str]) -> bool:
    """快速判断批量字符串是否有语义"""
    result = check_words_batch(strings)
    return result.has_meaning


# 4. 使用示例


# 测试示例（可选）
if __name__ == "__main__":
    # 单单词检测
    print(check_single_word("bestbuy"))  # True
    print(check_single_word("XFj-kC9uY"))  # False

    # 批量检测
    words = ["apple", "user123", "9X7ZpQ"]
    print(check_words_batch(words))  # [True, True, False]
