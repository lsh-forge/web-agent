import requests
from typing import Optional


def get_semantic_similarity(keyword: str, text: str) -> Optional[float]:
    """
    调用语义相似度接口，获取 keyword 和 text 的相似度

    Args:
        keyword: 关键词（接口参数）
        text: 待比较文本（接口参数）

    Returns:
        相似度值（float），失败则返回 None
    """
    # 接口地址
    url = "http://127.0.0.1:12346/compute_similarity"

    try:
        # 发送 GET 请求（带 Query 参数）
        resp = requests.get(
            url=url,
            params={"keyword": keyword, "text": text},  # 匹配接口的 Query 参数
            timeout=10
        )
        resp.raise_for_status()  # 非 2xx 状态码抛异常

        # 解析响应，提取相似度
        data = resp.json()
        if data.get("success"):
            return data.get("similarity")
        else:
            print(f"接口返回失败：{data.get('message')}")
            raise Exception

    except Exception as e:
        print(f"调用接口失败：{str(e)}")
        raise e

if __name__ == '__main__':
    value= get_semantic_similarity("hello","你好")
    print(value)

